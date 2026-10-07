import {
  createHashHistory, createRootRoute, createRoute, createRouter, Navigate, Outlet, redirect, useParams,
} from '@tanstack/react-router';
import { AdminPage, CapabilityPage, type Capability } from './pages/Capabilities';
import { HelpPage } from './components/Help';
import { HomePage } from './pages/Home';
import { EndpointPage, EndpointsPage } from './pages/Endpoints';
import { ServicesPage } from './pages/Services';
import { Layout } from './components/Layout';
import { ActivityPage } from './pages/Activity';
import { DeploymentPage } from './pages/Deployment';
import { DeploymentsPage } from './pages/Deployments';
import { JobPage, JobsPage } from './pages/Jobs';
import { JobRunPage } from './pages/JobRun';
import { ModelPage } from './pages/Model';
import { ModelsPage } from './pages/Models';
import { MonitorPage } from './pages/Monitor';
import { PipelinePage, PipelinesPage } from './pages/Pipelines';
import { PipelineRunPage } from './pages/PipelineRun';
import { ProjectPage } from './pages/Project';
import { ProjectsPage } from './pages/Projects';
import { RunsPage } from './pages/Runs';
import { SchedulePage, SchedulesPage } from './pages/Schedules';
import { SettingsPage } from './pages/Settings';

// The app lives under the hash (#/projects/...): the control plane serves one static page and
// needs no server-side routing. Query strings after the hash hold filters, so views are shareable.
const anySearch = (search: Record<string, unknown>) => search;

const root = createRootRoute({
  component: () => <Layout><Outlet /></Layout>,
  notFoundComponent: () => <Navigate to="/projects" replace />,
});

const index = createRoute({ getParentRoute: () => root, path: '/', beforeLoad: () => { throw redirect({ to: '/home' }); } });
const projects = createRoute({ getParentRoute: () => root, path: '/projects', component: ProjectsPage, validateSearch: anySearch });

const help = createRoute({ getParentRoute: () => root, path: '/help', component: HelpPage, validateSearch: anySearch });
const home = createRoute({ getParentRoute: () => root, path: '/home', component: HomePage });
const capabilities = (['runs', 'pipelines', 'models', 'functions', 'deployments', 'endpoints', 'activity'] as Capability[]).map((capability) => createRoute({
  getParentRoute: () => root, path: `/${capability}`, validateSearch: anySearch,
  component: () => <CapabilityPage key={capability} capability={capability} />,
}));
const services = createRoute({ getParentRoute: () => root, path: '/services', component: ServicesPage });
const admin = (['identity', 'settings'] as const).map((section) => createRoute({
  getParentRoute: () => root, path: `/${section}`, component: () => <AdminPage key={section} section={section} />,
}));

const schedules = createRoute({ getParentRoute: () => root, path: '/schedules', component: () => <SchedulesPage />, validateSearch: anySearch });
const schedule = createRoute({ getParentRoute: () => root, path: '/schedules/$id', component: function Page() { const { id } = useParams({ strict: false }); return <SchedulePage key={id} id={id!} />; } });
const monitor = createRoute({ getParentRoute: () => root, path: '/monitor', component: MonitorPage, validateSearch: anySearch });

/** Everything inside a project; its sections are in the content header. */
const project = createRoute({ getParentRoute: () => root, path: '/projects/$project', component: Outlet });
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
const functions = child('functions', (p) => <ModelsPage key={p.project} project={p.project!} functions />);
const fn = child('functions/$name', (p) => <ModelPage key={`${p.project}/${p.name}`} project={p.project!} name={p.name!} functions />);
const endpoints = child('endpoints', (p) => <EndpointsPage key={p.project} project={p.project!} />);
const endpoint = child('endpoints/$name', (p) => <EndpointPage key={`${p.project}/${p.name}`} project={p.project!} name={p.name!} />);
const deployments = child('deployments', (p) => <DeploymentsPage key={p.project} project={p.project!} />);
const deployment = child('deployments/$name', (p) => <DeploymentPage key={`${p.project}/${p.name}`} project={p.project!} name={p.name!} />);
const activity = child('activity', (p) => <ActivityPage key={p.project} project={p.project!} />);
const projectSchedules = child('schedules', (p) => <SchedulesPage key={p.project} project={p.project!} />);
const settings = child('settings', (p) => <SettingsPage key={p.project} project={p.project!} />);

const routeTree = root.addChildren([
  index, home, help, projects, monitor, schedules, schedule, services, ...capabilities, ...admin,
  project.addChildren([overview, runs, pipelineRun, jobRun, pipelines, pipeline, jobs, job, models, model, functions, fn, endpoints, endpoint, deployments, deployment, activity, projectSchedules, settings]),
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
