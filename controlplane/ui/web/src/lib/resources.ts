import type { S } from '../api/client';

/** Serving means the active revision, never a queued revision or a name heuristic. */
export function servingFor(model: S['ModelOut'], deployments: S['DeploymentOut'][]) {
  return deployments.flatMap((deployment) => {
    const revision = deployment.revisions.find((r) => r.revision === deployment.active_revision);
    return revision?.model === model.name ? [{ deployment, revision }] : [];
  });
}

export function functionStatus(serving: ReturnType<typeof servingFor>, fallback: string) {
  const statuses = serving.map(({ deployment }) => deployment.status);
  return ['DELETING', 'DELETED', 'FAILED', 'DEGRADED', 'DEPLOYING', 'PENDING', 'READY'].find((s) => statuses.includes(s as S['DeploymentStatus'])) ?? fallback;
}
