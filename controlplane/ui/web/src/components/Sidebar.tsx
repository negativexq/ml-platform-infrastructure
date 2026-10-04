import { useEffect, useRef } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useRouterState } from '@tanstack/react-router';
import { api, type S } from '../api/client';
import { go, routes } from '../lib/format';
import { HEALTH, usePlatformHealth } from '../lib/health';
import { GLOBAL_NAV, PROJECT_NAV, navigationContext } from '../lib/navigation';
import { Icon } from './icons';

export function useWhere() {
  return useRouterState({ select: (s) => navigationContext(s.location.pathname) });
}

export function Sidebar({ open }: { open: boolean }) {
  const where = useWhere();
  return (
    <aside id="sidebar" className={`sidebar${open ? ' open' : ''}`} aria-label="Main navigation">
      {GLOBAL_NAV.map((group) => <div key={group.label}>
        <div className="nav-label">{group.label}</div>
        <nav className="nav" aria-label={group.label}>
          {group.items.map(([key, label]) => <a key={key} href={`#/${key}`} data-nav={key}
            onClick={(event) => {
              if (key === 'projects' && where.project && where.section && event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) {
                event.preventDefault();
                go(`#/projects?section=${encodeURIComponent(where.section)}`);
              }
            }}
            aria-current={where.global === key ? 'page' : undefined}>
            <Icon name={key} />{label}{key === 'monitor' && <HealthMark />}
          </a>)}
        </nav>
      </div>)}
    </aside>
  );
}

/** The project context and lifecycle live in the main content, never in the sidebar. */
export function ProjectNavigation() {
  const { project, section } = useWhere();
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    root.current?.querySelectorAll('details').forEach((el) => { el.open = false; });
  }, [project, section]);
  useEffect(() => {
    const close = (event: MouseEvent | KeyboardEvent) => {
      if (event instanceof KeyboardEvent && event.key !== 'Escape') return;
      const target = event.target as Node;
      root.current?.querySelectorAll<HTMLDetailsElement>('details[open]').forEach((el) => {
        if (event instanceof KeyboardEvent || !el.contains(target)) {
          el.open = false;
          if (event instanceof KeyboardEvent) el.querySelector('summary')?.focus();
        }
      });
    };
    document.addEventListener('click', close);
    document.addEventListener('keydown', close);
    return () => { document.removeEventListener('click', close); document.removeEventListener('keydown', close); };
  }, []);
  if (!project) return null;
  return <div ref={root} className="project-context" data-testid="project-context">
    <ProjectLabel project={project} />
    <nav className="project-nav" aria-label="Project sections" data-testid="project-tabs">
      {PROJECT_NAV.map((group) => group.items ? <details key={group.key} data-project-group={group.key}>
        <summary data-active={group.items.some(([key]) => key === section) || undefined}>
          {group.label}<span aria-hidden="true"> ▾</span>
        </summary>
        <div className="project-subnav">
          {group.items.map(([key, label]) => <a key={key} href={`${routes.project(project)}/${key}`}
            aria-current={key === section ? 'page' : undefined} data-tab={key}>{label}</a>)}
        </div>
      </details> : <a key={group.key} href={`${routes.project(project)}${group.key ? `/${group.key}` : ''}`}
        aria-current={group.key === section ? 'page' : undefined} data-tab={group.key || 'overview'}>{group.label}</a>)}
    </nav>
  </div>;
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

function ProjectLabel({ project }: { project: string }) {
  const list = useQuery({ queryKey: ['projects', 'context'], queryFn: () => api.get<S['ProjectList']>('/projects?limit=200'), staleTime: 30_000 });
  const current = list.data?.items.find((p) => p.name === project);
  return <div className="project-label"><span className="muted">Project</span><strong>{current?.display_name ?? project}</strong></div>;
}
