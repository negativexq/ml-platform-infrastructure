import { api, ApiError, enc, type S } from '../api/client';
import { useOverlays } from './overlays';
import { go, routes } from '../lib/format';

const DEPLOYABLE = new Set(['CANDIDATE', 'CHAMPION']);
const NEW = '__new__';

type Preset = { deployment?: string; model?: string; version?: number };

/**
 * One dialog for "put this model version in front of traffic": into an existing deployment or
 * a new one, either as a canary (gated, reversible) or by replacing what is served.
 */
export function useDeploy(project: string) {
  const { form, toast } = useOverlays();
  const p = enc(project);
  return async (preset: Preset = {}) => {
    const [deployments, models] = await Promise.all([
      api.get<S['DeploymentList']>(`/projects/${p}/deployments`),
      api.get<S['ModelList']>(`/projects/${p}/models`),
    ]);
    const versions = (await Promise.all(models.items.map(async (m) => {
      const list = await api.get<{ items: { id: string; version: number; status: string }[] }>(`/projects/${p}/models/${enc(m.name)}/versions`);
      return list.items.filter((v) => DEPLOYABLE.has(v.status)).map((v) => ({ model: m.name, version: v.version, status: v.status }));
    }))).flat().sort((a, b) => a.model.localeCompare(b.model) || b.version - a.version);
    if (!versions.length) { toast('No version is ready to deploy: evaluate one until it becomes a candidate', 'bad'); return; }

    const target = preset.deployment ?? deployments.items[0]?.name ?? NEW;
    const chosen = preset.model ? `${preset.model}@${preset.version}` : `${versions[0]!.model}@${versions[0]!.version}`;
    const result = await form<string>({
      title: 'Deploy a model version', submitLabel: 'Deploy',
      intro: 'A canary moves traffic in steps and rolls back by itself if a gate fails. Replacing switches all traffic once the new revision is ready.',
      fields: [
        { name: 'version', label: 'Model version', required: true, value: chosen,
          options: versions.map((v) => ({ value: `${v.model}@${v.version}`, label: `${v.model} v${v.version} (${v.status.toLowerCase()})` })) },
        { name: 'deployment', label: 'Deployment', required: true, value: target,
          options: [...deployments.items.map((d) => ({ value: d.name, label: `${d.name} (${d.status.toLowerCase()}${d.active_revision != null ? `, serving r${d.active_revision}` : ''})` })),
            { value: NEW, label: 'New deployment…' }] },
        { name: 'new_name', label: 'New deployment name', placeholder: `${project}-staging`, pattern: '^([a-z][a-z0-9]*(-[a-z0-9]+)*)?$',
          hint: 'Only when “New deployment…” is chosen. Lowercase letters, digits and dashes.' },
        { name: 'mode', label: 'How', required: true, value: 'canary', options: [
          { value: 'canary', label: 'Canary rollout (recommended)' }, { value: 'replace', label: 'Replace what is served' }] },
        { name: 'steps', label: 'Canary steps (% of traffic)', placeholder: '10, 25, 50, 100', hint: 'Canary only. Ends at 100.' },
        { name: 'max_error_rate', label: 'Gate: max 5xx rate (%)', placeholder: '1' },
        { name: 'max_p95_latency_ms', label: 'Gate: max p95 latency (ms)', placeholder: '500' },
      ],
      submit: async (v) => {
        const [model, versionText] = (v.version ?? '').split('@') as [string, string];
        const version = Number(versionText);
        let name = v.deployment ?? '';
        if (name === NEW) {
          if (!v.new_name) throw new ApiError(422, 'invalid_argument', 'New deployment name: required');
          name = v.new_name;
          await api.post(`/projects/${p}/deployments`, { name });
        }
        const existing = deployments.items.find((d) => d.name === name);
        const canary = v.mode === 'canary' && existing?.active_revision != null;
        if (canary) {
          const gate: Record<string, number> = {};
          if (v.max_error_rate) gate.max_error_rate = Number(v.max_error_rate) / 100;
          if (v.max_p95_latency_ms) gate.max_p95_latency_ms = Number(v.max_p95_latency_ms);
          const steps = v.steps ? v.steps.split(/[\s,]+/).filter(Boolean).map(Number) : undefined;
          if (steps?.some(Number.isNaN)) throw new ApiError(422, 'invalid_argument', 'Canary steps: numbers separated by commas');
          await api.post(`/projects/${p}/deployments/${enc(name)}/rollouts`, { model, version, steps, gate: Object.keys(gate).length ? gate : undefined });
          toast(`Canary of ${model} v${version} started on ${name}`);
        } else {
          await api.post(`/projects/${p}/deployments/${enc(name)}/revisions`, { model, version });
          toast(v.mode === 'canary' ? `${name} serves nothing yet, so v${version} is deployed directly` : `Deploying ${model} v${version} to ${name}`);
        }
        return name;
      },
    });
    if (result) go(routes.deployment(project, result));
  };
}
