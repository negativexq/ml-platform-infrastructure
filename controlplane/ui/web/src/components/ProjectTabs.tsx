import { useRouterState } from '@tanstack/react-router';
import { routes } from '../lib/format';

const TABS: [string, string][] = [
  ['', 'Overview'], ['runs', 'Runs'], ['pipelines', 'Pipelines'], ['jobs', 'Jobs'], ['models', 'Models'],
  ['deployments', 'Deployments'], ['activity', 'Activity'], ['settings', 'Settings'],
];
// Detail pages belong to a tab: a pipeline run lives under Runs, a model under Models.
const OWNER: Record<string, string> = { 'pipeline-runs': 'runs', runs: 'runs' };

/** The project's sections. Always visible inside a project, so nothing is more than one click away. */
export function ProjectTabs({ project }: { project: string }) {
  const path = useRouterState({ select: (s) => s.location.pathname });
  const rest = path.split('/').slice(3); // ['', 'projects', name, section, ...]
  const section = rest[0] ? OWNER[rest[0]] ?? rest[0] : '';
  return (
    <nav className="tabs" aria-label="Project sections" data-testid="project-tabs">
      {TABS.map(([key, label]) => (
        <a key={key} href={`${routes.project(project)}${key ? `/${key}` : ''}`} aria-current={key === section ? 'page' : undefined}
          data-tab={key || 'overview'}>{label}</a>
      ))}
    </nav>
  );
}
