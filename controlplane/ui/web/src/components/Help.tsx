import { useState } from 'react';
import { routes } from '../lib/format';
import { useCrumbs } from '../lib/chrome';
import { useSearchState } from '../lib/search';
import { useWhere } from './Sidebar';

const TOPICS = [['start', 'Getting started'], ['map', 'Platform map'], ['page', 'Help for this page'], ['guides', 'How-to guides'], ['trouble', 'Troubleshooting'], ['shortcuts', 'Keyboard shortcuts']] as const;
const MAP = [
  ['home', 'Home', 'See what needs attention, running work and recent changes.'],
  ['projects', 'Projects', 'Choose a project card to work on its resources.'],
  ['runs', 'Runs', 'Inspect pipeline and job executions, logs and outcomes.'],
  ['pipelines', 'Pipelines', 'Reusable workflows that coordinate jobs.'],
  ['models', 'Models', 'Register ML models or LLMs, inspect versions and promote candidates.'],
  ['functions', 'Functions', 'Register container workloads with their own versions and scaling.'],
  ['deployments', 'Deployments', 'Choose what runs and manage serving revisions and rollouts.'],
  ['endpoints', 'Endpoints', 'Find the APIs exposed by deployments and try requests.'],
  ['services', 'Services', 'Check the health of connected platform services.'],
  ['monitor', 'Monitor', 'Inspect platform health and operational alerts.'],
  ['activity', 'Activity', 'Review who changed what and when.'],
  ['identity', 'Identity', 'Inspect your identity and project access.'],
  ['settings', 'Settings', 'Find platform settings and project administration.'],
] as const;

export function HelpContent({ initial = 'start', onNavigate }: { initial?: string; onNavigate?: () => void }) {
  const [topic, setTopic] = useState(initial);
  const where = useWhere();
  const section = where.project ? where.section || 'overview' : where.top;
  const entry = MAP.find(([key]) => key === section);
  const target = (key: string) => where.project && !['home', 'projects', 'services', 'monitor', 'identity'].includes(key)
    ? `${routes.project(where.project)}/${key}` : `#/${key}`;
  const link = (key: string, label: string) => <a href={target(key)} onClick={onNavigate}>{label}</a>;
  return <div className="help-content">
    <nav className="help-topics" aria-label="Help topics">{TOPICS.map(([key, label]) => <button key={key} className="btn small" type="button" aria-pressed={topic === key} onClick={() => setTopic(key)}>{label}</button>)}</nav>
    <div className="help-body">
      <h2>{TOPICS.find(([key]) => key === topic)?.[1] ?? 'Getting started'}</h2>
      {topic === 'start' && <><p>Start with a project: it groups your workflows, resources and APIs.</p><ol>
        <li>{link('projects', 'Open Projects')} and choose a card, or create a new project.</li>
        <li>Use Build to run pipelines and jobs. Watch execution results in Runs.</li>
        <li>Use Assets to register a model, LLM or function and add versions.</li>
        <li>Use Serve to deploy a version, then open its endpoint to test the API.</li>
      </ol><p>The left sidebar always shows the whole platform. A project’s Build, Assets and Serve navigation stays in its content header. To change projects, return to Projects and choose another card.</p></>}
      {topic === 'map' && <><p>Global lists show resources across your accessible projects. Open a resource to enter its project context.</p><table className="t"><thead><tr><th>Where</th><th>What you can do</th></tr></thead><tbody>{MAP.map(([key, label, text]) => <tr key={key}><td><a href={`#/${key}`} onClick={onNavigate}>{label}</a></td><td>{text}</td></tr>)}</tbody></table><p><b>Model / Function → Deployment → Endpoint</b>: a version is the asset, a deployment runs it, and an endpoint exposes its API.</p></>}
      {topic === 'page' && <><p><b>{where.project ? `${where.project} / ` : ''}{entry?.[1] ?? (section === 'home' ? 'Home' : section === 'overview' ? 'Project overview' : section)}</b></p><p>{entry?.[2] ?? (section === 'home' ? 'See what needs attention, currently running work, recent deployments and activity.' : section === 'overview' ? 'See this project’s resource counts, recent executions, serving health and activity.' : 'Open the platform map to find the capability you need. Resource detail pages show versions, status and the actions available to your role.')}</p><p>{where.project ? 'This view is scoped to the selected project. Use Projects to choose another project while keeping the current section.' : 'This is a platform view. Project filters narrow the list; opening a resource takes you to its project.'}</p></>}
      {topic === 'guides' && <>
        <h3>Run a workflow</h3><p>{link('pipelines', 'Open Pipelines')}, choose a registered pipeline and select Run. Pipelines are defined in code and registered through the API, usually from CI. Use Runs to inspect steps, logs and failures. Jobs can also be created and run in a project’s Build menu.</p>
        <h3>Register a model or LLM</h3><p>{link('models', 'Open Models')} in a project and select Register model. Choose Classic ML for registry versions, or LLM for GPU and context settings. Add a version, review its evaluation and promote an accepted candidate before serving.</p>
        <h3>Serve a function</h3><p>{link('functions', 'Open Functions')} in a project, register the workload and add its container image version. Functions have scaling and environment settings; they do not use model acceptance thresholds.</p>
        <h3>Deploy and test</h3><p>{link('deployments', 'Open Deployments')} in a project and select Deploy a version. Review the target and traffic change before submitting. Then {link('endpoints', 'open Endpoints')} to find the API, authentication instructions and request playground.</p>
        {!where.project && <p>Choose a project from each global list to access creation and deployment actions.</p>}
      </>}
      {topic === 'trouble' && <>
        <h3>Access and permissions</h3><p>A viewer can inspect resources. An operator can run and deploy workloads. A project admin manages members and project settings. If an action is disabled, its tooltip explains the required role. Ask the project admin to review your membership under the project’s Settings → Members. Platform administration requires a platform admin.</p>
        <h3>A run failed</h3><p>Open the failed run in {link('runs', 'Runs')}, read its status reason and step logs, and correct the failing job or inputs before running again.</p>
        <h3>An endpoint is unavailable</h3><p>Open its linked deployment to check the active revision and rollout status. Check {link('monitor', 'Monitor')} and {link('services', 'Services')} for platform issues.</p>
        <h3>A list is empty or cannot load</h3><p>Clear search, type, project and status filters first. A loading error is reported separately from an empty list; check connectivity and your access. When asking your administrator for help, include the project, resource or run ID and the displayed error.</p>
      </>}
      {topic === 'shortcuts' && <dl className="kv">{[['Ctrl/⌘ K', 'Search everything'], ['/', 'Focus this page’s search, or search everything'], ['g then p', 'Go to projects'], ['g then m', 'Go to the platform monitor'], ['?', 'Open Help & learning'], ['Esc', 'Close a dialog or menu']].map(([key, text]) => <span key={key} style={{ display: 'contents' }}><dt><kbd>{key}</kbd></dt><dd>{text}</dd></span>)}</dl>}
    </div>
  </div>;
}

export function HelpPage() {
  useCrumbs([{ label: 'Help & learning' }]);
  const [{ topic }] = useSearchState({ topic: 'start' });
  return <><h1>Help &amp; learning</h1><HelpContent key={topic} initial={topic} /></>;
}
