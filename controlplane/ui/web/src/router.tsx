import {
  createHashHistory, createRootRoute, createRoute, createRouter, Navigate, Outlet, redirect, useParams,
} from '@tanstack/react-router';
import { Layout } from './components/Layout';
import { ProjectTabs } from './components/ProjectTabs';
import { ActivityPage } from './pages/Activity';
import { DeploymentPage } from './pages/Deployment';
import { DeploymentsPage } from './pages/Deployments';
import { JobPage, JobsPage } from './pages/Jobs';
import { JobRunPage } from './pages/JobRun';
import { ModelPage } from './pages/Model';
import { ModelsPage } from './pages/Models';
import { PipelinePage, PipelinesPage } from './pages/Pipelines';
import { PipelineRunPage } from './pages/PipelineRun';
import { ProjectPage } from './pages/Project';
import { ProjectsPage } from './pages/Projects';
import { RunsPage } from './pages/Runs';
import { SettingsPage } from './pages/Settings';

// The app lives under the hash (#/projects/...): the control plane serves one static page and
// needs no server-side routing. Query strings after the hash hold filters, so views are shareable.
const anySearch = (search: Record<string, unknown>) => search;

const root = createRootRoute({
  component: () => <Layout><Outlet /></Layout>,
  notFoundComponent: () => <Navigate to="/projects" replace />,
});

const index = createRoute({ getParentRoute: () => root, path: '/', beforeLoad: () => { throw redirect({ to: '/projects' }); } });
const projects = createRoute({ getParentRoute: () => root, path: '/projects', component: ProjectsPage, validateSearch: anySearch });

/** Everything inside a project shares the section tabs. */
const project = createRoute({
  getParentRoute: () => root, path: '/projects/$project',
  component: function ProjectLayout() {
    const { project: p } = project.useParams();
    return <><ProjectTabs project={p} /><Outlet /></>;
  },
});
/** A page inside a project. It gets the route params (project, id, name, ...) as plain props. */
const child = (path: string, render: (params: Record<string, string>) => React.ReactNode) => createRoute({
  getParentRoute: () => project, path, validateSearch: anySearch,
  component: function Page() {
    return <>{render(useParams({ strict: false }) as Record<string, string>)}</>;
  },
});

const overview = child('/', (p) => <ProjectPage key={p.project} name={p.project!} />);
const runs = child('runs', (p) => <RunsPage key={p.project} project={p.project!} />);
const pipelineRun = child('pipeline-runs/$id', (p) => <PipelineRunPage key={p.id} project={p.project!} id={p.id!} />);
const jobRun = child('runs/$id', (p) => <JobRunPage key={p.id} project={p.project!} id={p.id!} />);
const pipelines = child('pipelines', (p) => <PipelinesPage key={p.project} project={p.project!} />);
const pipeline = child('pipelines/$name', (p) => <PipelinePage key={`${p.project}/${p.name}`} project={p.project!} name={p.name!} />);
const jobs = child('jobs', (p) => <JobsPage key={p.project} project={p.project!} />);
const job = child('jobs/$job', (p) => <JobPage key={`${p.project}/${p.job}`} project={p.project!} name={p.job!} />);
const models = child('models', (p) => <ModelsPage key={p.project} project={p.project!} />);
const model = child('models/$name', (p) => <ModelPage key={`${p.project}/${p.name}`} project={p.project!} name={p.name!} />);
const deployments = child('deployments', (p) => <DeploymentsPage key={p.project} project={p.project!} />);
const deployment = child('deployments/$name', (p) => <DeploymentPage key={`${p.project}/${p.name}`} project={p.project!} name={p.name!} />);
const activity = child('activity', (p) => <ActivityPage key={p.project} project={p.project!} />);
const settings = child('settings', (p) => <SettingsPage key={p.project} project={p.project!} />);

const routeTree = root.addChildren([
  index, projects,
  project.addChildren([overview, runs, pipelineRun, jobRun, pipelines, pipeline, jobs, job, models, model, deployments, deployment, activity, settings]),
]);
// Plain `?key=value` query strings (every value is a string), rather than JSON-encoded ones, so
// shared links stay readable: `#/projects/x/runs?status=failed&kind=job`.
const parseSearch = (search: string) => Object.fromEntries(new URLSearchParams(search.startsWith('?') ? search.slice(1) : search));
const stringifySearch = (search: Record<string, unknown>) => {
  const query = new URLSearchParams(
    Object.entries(search).filter(([, v]) => v != null && v !== '').map(([k, v]) => [k, String(v)]),
  ).toString();
  return query ? `?${query}` : '';
};

export const router = createRouter({ routeTree, history: createHashHistory(), defaultPreload: false, parseSearch, stringifySearch });

declare module '@tanstack/react-router' {
  interface Register { router: typeof router }
}
