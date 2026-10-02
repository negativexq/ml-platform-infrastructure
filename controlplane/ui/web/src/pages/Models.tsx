import { api, ApiError, enc, type S } from '../api/client';
import { Badge, Empty, Table, Time } from '../components/bits';
import { useOverlays } from '../components/overlays';
import { useCrumbs } from '../lib/chrome';
import { go, num, parseThresholds, routes } from '../lib/format';
import { QueryView, useLiveQuery } from '../lib/query';

type Row = { model: S['ModelOut']; versions: S['ModelVersionOut'][] };

export function ModelsPage({ project }: { project: string }) {
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
      intro: 'Versions are discovered from the registry; the acceptance thresholds decide which ones may become candidates.',
      fields: [
        { name: 'name', label: 'Name', required: true, pattern: '^[a-z][a-z0-9]*(-[a-z0-9]+)*$', placeholder: 'scorer', hint: 'Lowercase letters, digits and dashes.' },
        { name: 'thresholds', label: 'Acceptance thresholds', type: 'textarea', placeholder: 'auc >= 0.9\nrmse <= 0.3',
          hint: 'One per line: metric >= number or metric <= number. A version that misses one is rejected.' },
      ],
      submit: (v) => {
        let thresholds;
        try { thresholds = parseThresholds(v.thresholds ?? ''); } catch (e) { throw new ApiError(422, 'invalid_argument', e instanceof Error ? e.message : 'invalid'); }
        return api.post<S['ModelOut']>(`/projects/${p}/models`, { name: v.name, thresholds });
      },
    });
    if (model) { toast(`Model ${model.name} registered`); go(routes.model(project, model.name)); }
  }

  return (
    <QueryView query={query}>
      {(rows) => (
        <>
          <div className="page-head">
            <h1>Models</h1>
            <div className="actions"><button className="btn primary" type="button" data-testid="register-model" onClick={register}>Register model</button></div>
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
                    <td><a href={routes.model(project, model.name)}>{model.name}</a>{model.alias_drift && <> <Badge status="DRIFTED" /></>}</td>
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
