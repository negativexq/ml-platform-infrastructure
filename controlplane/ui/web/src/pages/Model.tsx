import { useState } from 'react';
import { api, ApiError, enc, type S } from '../api/client';
import { Alert, Badge, Empty, Table, Time } from '../components/bits';
import { useDeploy } from '../components/Deploy';
import { useOverlays } from '../components/overlays';
import { useCrumbs } from '../lib/chrome';
import { formatThresholds, lowerIsBetter, num, parseThresholds, routes, shortId, type Thresholds } from '../lib/format';
import { QueryView, useAct, useLiveQuery } from '../lib/query';

const EVALUATABLE = new Set(['REGISTERED', 'EVALUATING']);
type Version = S['ModelVersionOut'];

const metricNames = (versions: Version[]) =>
  [...new Set(versions.flatMap((v) => v.evaluations.flatMap((e) => Object.keys(e.metrics))))].sort();
const latest = (v: Version) => v.evaluations[v.evaluations.length - 1];

export function ModelPage({ project, name }: { project: string; name: string }) {
  useCrumbs([{ label: 'Projects', href: routes.projects() }, { label: project, href: routes.project(project) },
    { label: 'Models', href: `${routes.project(project)}/models` }, { label: name }]);
  const base = `/projects/${enc(project)}/models/${enc(name)}`;
  const { confirm, toast, form } = useOverlays();
  const deploy = useDeploy(project);
  const act = useAct();
  const [picked, setPicked] = useState<string | null>(null);

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
    });
    if (done) { toast('Thresholds saved'); await query.refetch(); }
  }

  return (
    <QueryView query={query}>
      {({ model, versions }) => {
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
                <button className="btn" data-testid="discover" onClick={() => act(async () => {
                  const r = await api.post<{ created: unknown[] }>(`${base}/discover`);
                  toast(r.created.length ? `Registered ${r.created.length} new version(s)` : 'No new versions found');
                }, 'Checked the registry')}>Discover versions</button>
                <button className="btn" data-testid="edit-thresholds" onClick={() => editThresholds(model.thresholds as Thresholds)}>Edit thresholds</button>
              </div>
            </div>
            <p className="sub">
              Registry name <span className="mono">{model.registry_name}</span>{' · acceptance thresholds '}
              {Object.keys(model.thresholds).length
                ? Object.entries(model.thresholds).map(([m, t]) => (
                  <span className="chip mono" key={m}>{`${m} ${t.min != null ? '≥ ' + t.min : ''}${t.min != null && t.max != null ? ', ' : ''}${t.max != null ? '≤ ' + t.max : ''}`}</span>))
                : <span className="muted">none set</span>}
            </p>
            {model.alias_drift && <Alert>{`Registry alias drift: ${model.alias_drift}`}</Alert>}
            {versions.length ? (
              <Table testid="versions" head={['Version', 'Status', ...metrics, 'Source run', '']}>
                {versions.map((v) => {
                  const ev = latest(v);
                  return (
                    <tr key={v.id} className={`click${v.id === selected ? ' sel' : ''}`} data-testid="version-row" data-version={v.version} onClick={() => setPicked(v.id)}>
                      <td className="mono">{`v${v.version}`}</td><td><Badge status={v.status} /></td>
                      {metrics.map((m) => <td key={m} className="num mono"><Metric value={ev?.metrics[m]} base={v.id === champion?.id ? undefined : championEval?.metrics[m]}
                        lower={lowerIsBetter(m, model.thresholds as Thresholds)} /></td>)}
                      <td>{v.source_pipeline_run_id
                        ? <a href={routes.pipelineRun(project, v.source_pipeline_run_id)} onClick={(e) => e.stopPropagation()}>{shortId(v.source_pipeline_run_id)}</a> : '—'}</td>
                      <td className="num">
                        {EVALUATABLE.has(v.status) && (
                          <button className="btn small" data-testid="evaluate" onClick={(e) => { e.stopPropagation(); act(() => api.post(`/model-versions/${v.id}/evaluate`), `Evaluated v${v.version}`); }}>Evaluate</button>)}
                        {v.status === 'CANDIDATE' && (
                          <button className="btn small primary" data-testid="promote" onClick={async (e) => {
                            e.stopPropagation();
                            const body = champion ? `v${champion.version} (current champion) will be archived and v${v.version} becomes the champion.` : `v${v.version} becomes the first champion.`;
                            if (await confirm({ title: `Promote v${v.version}?`, body: `${body} The registry alias follows shortly.`, confirmLabel: 'Promote' }))
                              await act(() => api.post(`/model-versions/${v.id}/promote`), `v${v.version} is now the champion`);
                          }}>Promote</button>)}
                        {(v.status === 'CANDIDATE' || v.status === 'CHAMPION') && (
                          <button className="btn small" data-testid="deploy-version" title="Deploy this version, as a canary or directly"
                            onClick={(e) => { e.stopPropagation(); deploy({ model: name, version: v.version }); }}>Deploy</button>)}
                      </td>
                    </tr>);
                })}
              </Table>
            ) : <Empty>No versions yet. Train and register one, then press “Discover versions”.</Empty>}
            {detail && <Detail v={detail} />}
          </>
        );
      }}
    </QueryView>
  );
}

function Detail({ v }: { v: Version }) {
  return (
    <div className="cols section" data-testid="version-detail">
      <div className="card">
        <h2>{`v${v.version} · evaluations`}</h2>
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
