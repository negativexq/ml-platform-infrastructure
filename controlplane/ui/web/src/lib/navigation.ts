/** Global capabilities never depend on the selected project. */
export const GLOBAL_NAV: { label: string; items: [string, string][] }[] = [
  { label: 'Overview', items: [['home', 'Home']] },
  { label: 'Work', items: [['projects', 'Projects'], ['runs', 'Runs'], ['pipelines', 'Pipelines']] },
  { label: 'AI', items: [['models', 'Models'], ['functions', 'Functions']] },
  { label: 'Serving', items: [['deployments', 'Deployments'], ['endpoints', 'Endpoints']] },
  { label: 'Platform', items: [['services', 'Services']] },
  { label: 'Operations', items: [['monitor', 'Monitor'], ['activity', 'Activity']] },
  { label: 'Admin', items: [['identity', 'Identity'], ['settings', 'Settings']] },
];
export const PROJECT_NAV: { label: string; key: string; items?: [string, string][] }[] = [
  { key: '', label: 'Overview' },
  { key: 'build', label: 'Build', items: [['runs', 'Runs'], ['pipelines', 'Pipelines'], ['jobs', 'Jobs']] },
  { key: 'assets', label: 'Assets', items: [['models', 'Models'], ['functions', 'Functions']] },
  { key: 'serve', label: 'Serve', items: [['deployments', 'Deployments'], ['endpoints', 'Endpoints']] },
  { key: 'activity', label: 'Activity' }, { key: 'settings', label: 'Settings' },
];
export const resourceType = (kind: string) => kind === 'function' ? 'Function' : kind === 'llm' ? 'LLM' : 'ML Model';
export function navigationContext(path: string) {
  const [, top = '', name, first] = path.split('/');
  const project = top === 'projects' && name ? decodeURIComponent(name) : null;
  const section = first === 'pipeline-runs' ? 'runs' : first ?? '';
  // A project is entered through Projects; its lifecycle stays in the content tabs.
  return { top, project, section, global: project ? 'projects' : top || 'home' };
}
