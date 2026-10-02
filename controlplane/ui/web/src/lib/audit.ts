import { routes } from './format';

/** Audit actions in words a person would use. Unknown ones fall back to the raw action. */
const LABELS: Record<string, string> = {
  'project.created': 'Project created', 'project.provisioning': 'Provisioning the namespace', 'project.provisioned': 'Namespace provisioned',
  'project.failed': 'Provisioning failed', 'project.drift_detected': 'Namespace drift detected', 'project.delete_requested': 'Deletion requested',
  'project.deleted': 'Project deleted', 'project.delete_blocked': 'Deletion blocked',
  'job.created': 'Job defined', 'pipeline.created': 'Pipeline registered',
  'run.created': 'Job run created', 'run.submitted': 'Job run submitted', 'run.running': 'Job run started', 'run.succeeded': 'Job run succeeded',
  'run.failed': 'Job run failed', 'run.cancel_requested': 'Job run cancellation requested', 'run.cancelled': 'Job run cancelled',
  'pipeline_run.created': 'Pipeline run created', 'pipeline_run.submitted': 'Pipeline run submitted', 'pipeline_run.running': 'Pipeline run started',
  'pipeline_run.succeeded': 'Pipeline run succeeded', 'pipeline_run.failed': 'Pipeline run failed', 'pipeline_run.cancel_requested': 'Pipeline run cancellation requested',
  'pipeline_run.cancelled': 'Pipeline run cancelled',
  'model.created': 'Model registered', 'model.thresholds_changed': 'Acceptance thresholds changed',
  'model.alias_drift_detected': 'Registry alias drift detected', 'model.alias_synced': 'Registry alias synced',
  'model_version.registered': 'Version registered', 'model_version.candidate': 'Version passed evaluation',
  'model_version.promoted': 'Version promoted to champion', 'model_version.rejected': 'Version rejected by evaluation',
  'model_version.archived': 'Version archived',
  'evaluation.started': 'Evaluation started', 'evaluation.passed': 'Evaluation passed', 'evaluation.failed': 'Evaluation failed',
  'step_run.running': 'Step started', 'step_run.succeeded': 'Step succeeded', 'step_run.failed': 'Step failed', 'step_run.skipped': 'Step skipped',
  'deployment.created': 'Deployment created', 'deployment.revision_created': 'New revision', 'deployment.ready': 'Deployment ready',
  'deployment.drift_detected': 'Serving drift detected', 'deployment.redeploying': 'Recreating serving resource',
  'deployment.failed': 'Deployment failed', 'deployment.rolled_back': 'Deployment rolled back', 'endpoint.ready': 'Endpoint ready',
  'rollout.started': 'Canary started', 'rollout.step_applied': 'Canary traffic increased', 'rollout.observing': 'Canary under observation',
  'rollout.succeeded': 'Canary promoted', 'rollout.rolled_back': 'Canary rolled back', 'rollout.abort_requested': 'Canary abort requested',
  'endpoint.exposure_changed': 'Endpoint exposure changed', 'api_key.created': 'API key issued', 'api_key.revoked': 'API key revoked',
  'membership.granted': 'Member added', 'membership.changed': 'Member role changed', 'membership.revoked': 'Member removed',
};

export const describeAction = (action: string) => LABELS[action] ?? action.replace(/[._]/g, ' ');
/** Bookkeeping a person rarely needs: per-step progress, canary ticks, hand-offs between states. */
export const isRoutine = (e: { entity_type: string; action: string }) =>
  e.entity_type === 'step_run' || /^(rollout\.(observing|step_applied)|evaluation\.started|model\.alias_synced)$/.test(e.action)
  || /\.(submitted|running|provisioning)$/.test(e.action);

export const isFailure = (action: string) => /\.(failed|rolled_back|rejected|drift_detected|alias_drift_detected|delete_blocked)$/.test(action);

/** Where an audited entity can be looked at, when the UI has a page for it. */
export function entityHref(project: string, e: { entity_type: string; entity_id: string; payload: Record<string, unknown> }, names: Map<string, string>) {
  switch (e.entity_type) {
    case 'pipeline_run': return routes.pipelineRun(project, e.entity_id);
    case 'run': return routes.jobRun(project, e.entity_id);
    case 'project': return routes.project(project);
    case 'deployment': { const n = names.get(e.entity_id); return n ? routes.deployment(project, n) : null; }
    case 'model': { const n = names.get(e.entity_id); return n ? routes.model(project, n) : null; }
    default: return null;
  }
}
