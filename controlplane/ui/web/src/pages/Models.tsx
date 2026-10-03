import { api, ApiError, enc, type S } from '../api/client';
import { Badge, Empty, Table, Time } from '../components/bits';
import { useOverlays } from '../components/overlays';
import { useAccess } from '../lib/me';
import { useCrumbs } from '../lib/chrome';
import { go, num, parsePairs, parseThresholds, routes } from '../lib/format';
import { QueryView, useLiveQuery } from '../lib/query';

type Row = { model: S['ModelOut']; versions: S['ModelVersionOut'][] };

export function ModelsPage({ project }: { project: string }) {
  const access = useAccess(project);
  useCrumbs([{ label: 'Projects', href: routes.projects() }, { label: project, href: routes.project(project) }, { label: 'Models' }]);
  const p = enc(project);
  const { form, toast } = useOverlays();
  const query = useLiveQuery<Row[]>(['models', project], async () => {
    const { items } = await api.get<S['ModelList']>(`/projects/${p}/models`);
    return Promise.all(items.map(async (model) => {
      const list = await api.get<{ items: { id: string }[] }>(`/projects/${p}/models/${enc(model.name)}/versions`);
      const versions = await Promise.all(list.items.map((v) => api.get<S['ModelVersionOut']>(`/model-versions/${v.id}`)));
      return { model, versions: versions.sort((a, b) => b.version - a.version) };
    }));
  }, (rows) => rows.some((r) => r.versions.some((v) => v.status === 'EVALUATING')));

  async function register() {
    const model = await form<S['ModelOut']>({
      title: 'Register a model', submitLabel: 'Register',
      intro: 'Versions come from the registry (or, for an LLM, from the Hugging Face Hub); the acceptance thresholds decide which ones may become candidates.',
      fields: [
        { name: 'name', label: 'Name', required: true, pattern: '^[a-z][a-z0-9]*(-[a-z0-9]+)*$', placeholder: 'scorer', hint: 'Lowercase letters, digits and dashes.' },
        { name: 'kind', label: 'Kind', required: true, value: 'classic', options: [
          { value: 'classic', label: 'Classic model (predictive, from the registry)' }, { value: 'llm', label: 'LLM (served on GPUs, chat API)' },
          { value: 'function', label: 'Function (your own container, scales to zero)' }] },
        { name: 'gpus', label: 'GPUs per replica', value: '1', pattern: '[1-8]', hint: 'LLMs only. Taken from the project’s GPU quota; a canary needs them twice.' },
        { name: 'context', label: 'Context length (tokens)', pattern: '[0-9]{3,7}', placeholder: 'the model’s own', hint: 'LLMs only. Longer contexts need more GPU memory.' },
        { name: 'scale', label: 'Replicas (min-max)', value: '0-3', pattern: '[0-9]{1,2}-[0-9]{1,2}', hint: 'Functions only. A minimum of 0 scales to zero when idle; the first call then waits for a cold start.' },
        { name: 'concurrency', label: 'Requests per replica at once', value: '10', pattern: '[0-9]{1,4}', hint: 'Functions only. More replicas start when this is reached.' },
        { name: 'env', label: 'Environment', type: 'textarea', placeholder: 'QUEUE=tier1', hint: 'Functions only. One KEY=value per line. No secrets here: they are visible to project members.' },
        { name: 'thresholds', label: 'Acceptance thresholds', type: 'textarea', placeholder: 'auc >= 0.9\nrmse <= 0.3',
          hint: 'One per line: metric >= number or metric <= number. A version that misses one is rejected.' },
      ],
      submit: (v) => {
        let thresholds;
        try { thresholds = parseThresholds(v.thresholds ?? ''); } catch (e) { throw new ApiError(422, 'invalid_argument', e instanceof Error ? e.message : 'invalid'); }
        const llm = v.kind === 'llm';
        let fn = null;
        if (v.kind === 'function') {
          const [min, max] = (v.scale || '0-3').split('-').map(Number);
          let env: Record<string, string>;
          try { env = parsePairs(v.env ?? ''); } catch (e) { throw new ApiError(422, 'invalid_argument', e instanceof Error ? e.message : 'invalid'); }
          fn = { min_scale: min, max_scale: max, concurrency: Number(v.concurrency || 10), env };
          if (Object.keys(thresholds).length) throw new ApiError(422, 'invalid_argument', 'A function has no acceptance thresholds: leave them empty');
        }
        return api.post<S['ModelOut']>(`/projects/${p}/models`, {
          name: v.name, thresholds, kind: v.kind,
          llm: llm ? { gpus: Number(v.gpus || 1), context_length: v.context ? Number(v.context) : null } : null,
          function: fn,
        });
      },
      preview: (v) => (v.kind === 'function'
        ? <p>{`${v.name || 'This function'} runs your container image: ${v.scale || '0-3'} replicas, ${v.concurrency || 10} requests each at once${(v.scale || '0-3').startsWith('0-') ? ', down to none when idle' : ''}. Its versions are images, deployable at once; callers use POST …/invoke with any JSON.`}</p>
        : v.kind === 'llm'
        ? <p>{`${v.name || 'This model'} is an LLM: each serving replica holds ${v.gpus || 1} GPU${v.gpus === '1' || !v.gpus ? '' : 's'} of the project's quota, and it answers OpenAI-style chat completions. Register its versions from the Hugging Face Hub with the results of your own evaluation.`}</p>
        : <p>{`${v.name || 'This model'} is served by the platform's model server; its versions are discovered from the registry after training.`}</p>),
    });
    if (model) { toast(`Model ${model.name} registered`); go(routes.model(project, model.name)); }
  }

  return (
    <QueryView query={query}>
      {(rows) => (
        <>
          <div className="page-head">
            <h1>Models</h1>
            <div className="actions"><button className="btn primary" type="button" data-testid="register-model" disabled={!access.may('operator')} title={access.why('operator')} onClick={register}>Register model</button></div>
          </div>
          <p className="sub">What is serving, what is waiting to be promoted, and what was turned away.</p>
          {rows.length === 0 ? <Empty>No models yet. Register one, then train and discover versions.</Empty> : (
            <Table testid="models" head={['Model', 'Champion', 'Waiting for promotion', 'Rejected', 'Versions', 'Last change']}>
              {rows.map(({ model, versions }) => {
                const champion = versions.find((v) => v.status === 'CHAMPION');
                const candidates = versions.filter((v) => v.status === 'CANDIDATE');
                const metric = Object.keys(model.thresholds)[0];
                const ev = champion?.evaluations[champion.evaluations.length - 1];
                return (
                  <tr key={model.id} className="click" data-testid="model-row" onClick={() => go(routes.model(project, model.name))}>
                    <td><a href={routes.model(project, model.name)}>{model.name}</a>{model.kind === 'llm' && <span className="chip model-kind" data-testid="kind-llm">LLM</span>}{model.kind === 'function' && <span className="chip model-kind" data-testid="kind-function">Function</span>}{model.alias_drift && <> <Badge status="DRIFTED" /></>}</td>
                    <td>{champion ? <><Badge status="CHAMPION" />{` v${champion.version}`}{metric && ev?.metrics[metric] != null && <span className="muted small">{` · ${metric} ${num(ev.metrics[metric], 3)}`}</span>}</> : <span className="muted">none</span>}</td>
                    <td>{candidates.length ? candidates.map((v) => <span className="chip" key={v.id}>{`v${v.version}`}</span>) : '—'}</td>
                    <td className="num">{versions.filter((v) => v.status === 'REJECTED').length}</td>
                    <td className="num">{versions.length}</td>
                    <td>{versions[0] ? <Time iso={versions[0].updated_at} /> : '—'}</td>
                  </tr>);
              })}
            </Table>
          )}
        </>
      )}
    </QueryView>
  );
}
