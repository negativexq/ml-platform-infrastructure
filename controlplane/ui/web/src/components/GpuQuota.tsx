import { useQuery } from '@tanstack/react-query';
import { api, enc, type S } from '../api/client';
import { useMe } from '../lib/me';
import { useAct } from '../lib/query';
import { useOverlays } from './overlays';

/**
 * How many GPUs the project may hold and how many its LLMs hold now. GPUs are shared by the
 * whole platform, so only platform admins change the quota; everyone else sees who to ask.
 */
export function GpuQuota({ project }: { project: string }) {
  const p = enc(project);
  const me = useMe().data;
  const { form } = useOverlays();
  const act = useAct();
  const query = useQuery({ queryKey: ['summary', project], queryFn: () => api.get<S['SummaryOut']>(`/projects/${p}/summary`) });
  const s = query.data;
  const admin = Boolean(me?.platform_admin);

  async function change(current: S['SummaryOut']) {
    const done = await form({
      title: 'GPU quota', submitLabel: 'Save quota',
      intro: 'GPUs the project’s LLMs may hold together. A serving replica holds its model’s GPUs; during a canary the old and the new revision both hold theirs.',
      fields: [{ name: 'gpus', label: 'GPUs', required: true, pattern: '[0-9]{1,2}', value: String(current.gpu_quota) }],
      preview: (v) => {
        const gpus = Number(v.gpus);
        if (!Number.isFinite(gpus) || v.gpus === '') return null;
        if (gpus < current.gpus_in_use) {
          return <p className="fail">{`${current.gpus_in_use} GPUs are in use: the platform refuses a quota below that. Scale down or stop an LLM deployment first.`}</p>;
        }
        return (
          <p className="pass">{gpus === 0
            ? 'No GPUs: the project cannot serve LLMs. Classic models are not affected.'
            : `${gpus - current.gpus_in_use} GPU${gpus - current.gpus_in_use === 1 ? '' : 's'} free for new LLM deployments and canaries. The namespace quota (with CPU and memory per GPU) follows within a reconcile pass.`}</p>
        );
      },
      submit: (v) => api.put(`/projects/${p}/gpu-quota`, { gpus: Number(v.gpus) }),
    });
    if (done !== null) await act(async () => undefined, 'GPU quota saved');
  }

  return (
    <div className="section card" data-testid="gpu-quota">
      <div className="section-head">
        <h2>GPUs</h2>
        {s && (
          <button className="btn small" type="button" data-testid="change-gpu-quota" disabled={!admin}
            title={admin ? 'Change how many GPUs this project may hold' : 'Only platform admins change GPU quotas'} onClick={() => change(s)}>Change quota</button>)}
      </div>
      {!s ? <p className="muted">Loading…</p> : (
        <>
          <p data-testid="gpu-use">
            <b>{`${s.gpus_in_use} of ${s.gpu_quota}`}</b>{` GPU${s.gpu_quota === 1 ? '' : 's'} in use`}
            {s.gpu_quota === 0 && <span className="muted">: this project cannot serve LLMs until a platform admin grants GPUs.</span>}
          </p>
          {s.gpu_quota > 0 && (
            <div className="meter-track" role="img" aria-label={`${s.gpus_in_use} of ${s.gpu_quota} GPUs in use`}>
              <div className="meter-fill" style={{ width: `${Math.min(100, (100 * s.gpus_in_use) / s.gpu_quota)}%`, background: 'var(--series-1)' }} />
            </div>)}
        </>)}
    </div>
  );
}
