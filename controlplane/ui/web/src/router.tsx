import {
  createHashHistory, createRootRoute, createRoute, createRouter, Navigate, Outlet, redirect,
} from '@tanstack/react-router';
import { Layout } from './components/Layout';
import { DeploymentPage } from './pages/Deployment';
import { JobRunPage } from './pages/JobRun';
import { ModelPage } from './pages/Model';
import { PipelineRunPage } from './pages/PipelineRun';
import { ProjectPage } from './pages/Project';
import { ProjectsPage } from './pages/Projects';

// The app lives under the hash (#/projects/...): the control plane serves one static page and
// needs no server-side routing.
const root = createRootRoute({
  component: () => <Layout><Outlet /></Layout>,
  notFoundComponent: () => <Navigate to="/projects" replace />,
});

const index = createRoute({ getParentRoute: () => root, path: '/', beforeLoad: () => { throw redirect({ to: '/projects' }); } });
const projects = createRoute({ getParentRoute: () => root, path: '/projects', component: ProjectsPage });
const project = createRoute({
  getParentRoute: () => root, path: '/projects/$project',
  component: function Component() { return <ProjectPage key={project.useParams().project} name={project.useParams().project} />; },
});
const pipelineRun = createRoute({
  getParentRoute: () => root, path: '/projects/$project/pipeline-runs/$id',
  component: function Component() {
    const { project: p, id } = pipelineRun.useParams();
    return <PipelineRunPage key={id} project={p} id={id} />;
  },
});
const jobRun = createRoute({
  getParentRoute: () => root, path: '/projects/$project/runs/$id',
  component: function Component() {
    const { project: p, id } = jobRun.useParams();
    return <JobRunPage key={id} project={p} id={id} />;
  },
});
const model = createRoute({
  getParentRoute: () => root, path: '/projects/$project/models/$name',
  component: function Component() {
    const { project: p, name } = model.useParams();
    return <ModelPage key={`${p}/${name}`} project={p} name={name} />;
  },
});
const deployment = createRoute({
  getParentRoute: () => root, path: '/projects/$project/deployments/$name',
  component: function Component() {
    const { project: p, name } = deployment.useParams();
    return <DeploymentPage key={`${p}/${name}`} project={p} name={name} />;
  },
});

const routeTree = root.addChildren([index, projects, project, pipelineRun, jobRun, model, deployment]);
export const router = createRouter({ routeTree, history: createHashHistory(), defaultPreload: false });

declare module '@tanstack/react-router' {
  interface Register { router: typeof router }
}
