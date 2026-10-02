import { useQuery } from '@tanstack/react-query';
import { useRouterState } from '@tanstack/react-router';
import { api, type S } from '../api/client';
import { go, routes } from '../lib/format';
import { HEALTH, usePlatformHealth } from '../lib/health';
import { Icon } from './icons';

const SECTIONS: [string, string][] = [
  ['', 'Overview'], ['runs', 'Runs'], ['pipelines', 'Pipelines'], ['jobs', 'Jobs'], ['models', 'Models'],
  ['deployments', 'Deployments'], ['activity', 'Activity'], ['settings', 'Settings'],
];
// Detail pages belong to a section: a pipeline run lives under Runs, a model under Models.
const OWNER: Record<string, string> = { 'pipeline-runs': 'runs', runs: 'runs' };

/** Where the location is: top-level page, project, and the project section that owns it. */
export function useWhere() {
  const path = useRouterState({ select: (s) => s.location.pathname });
  const [, top = '', name, first] = path.split('/');
  const project = top === 'projects' && name ? decodeURIComponent(name) : null;
  const section = first ? OWNER[first] ?? first : '';
  return { top, project, section };
}

/**
 * The app's navigation, always in the same place: platform pages first, then the current
 * project's sections under a project switcher. On a phone it is a drawer behind the menu button.
 */
export function Sidebar({ open }: { open: boolean }) {
  const { top, project, section } = useWhere();
  return (
    <aside id="sidebar" className={`sidebar${open ? ' open' : ''}`} aria-label="Main navigation">
      <div className="nav-label">Platform</div>
      <nav className="nav" aria-label="Platform">
        <a href={routes.projects()} data-nav="projects" aria-current={top === 'projects' && !project ? 'page' : undefined}>
          <Icon name="projects" />Projects
        </a>
        <a href={routes.monitor()} data-nav="monitor" aria-current={top === 'monitor' ? 'page' : undefined}>
          <Icon name="monitor" />Monitor<HealthMark />
        </a>
      </nav>
      {project && (
        <>
          <ProjectSwitcher project={project} section={section} />
          <nav className="nav" aria-label="Project sections" data-testid="project-tabs">
            {SECTIONS.map(([key, label]) => (
              <a key={key} href={`${routes.project(project)}${key ? `/${key}` : ''}`} aria-current={key === section ? 'page' : undefined}
                data-tab={key || 'overview'}><Icon name={key || 'overview'} />{label}</a>
            ))}
          </nav>
        </>
      )}
    </aside>
  );
}

/** Only speaks up when something is wrong, so a quiet sidebar means a healthy platform. */
function HealthMark() {
  const health = usePlatformHealth(15, 60_000);
  const status = health.data?.status;
  if (status !== 'warning' && status !== 'critical') return null;
  const h = HEALTH[status];
  return (
    <span className="nav-status" style={{ color: h.color }} title={`Platform health: ${h.word}`} data-testid="nav-health">
      <span aria-hidden="true">{h.symbol}</span><span className="sr">{h.word}</span>
    </span>
  );
}

function ProjectSwitcher({ project, section }: { project: string; section: string }) {
  const list = useQuery({ queryKey: ['projects', 'switcher'], queryFn: () => api.get<S['ProjectList']>('/projects?limit=200'), staleTime: 30_000 });
  const items = list.data?.items ?? [];
  const known = items.some((p) => p.name === project);
  return (
    <div className="switcher">
      <label className="nav-label" htmlFor="project-switcher">Project</label>
      <select id="project-switcher" data-testid="project-switcher" value={project}
        onChange={(e) => go(`${routes.project(e.target.value)}${section ? `/${section}` : ''}`)}>
        {!known && <option value={project}>{project}</option>}
        {items.map((p) => <option key={p.name} value={p.name}>{p.display_name}</option>)}
      </select>
    </div>
  );
}
