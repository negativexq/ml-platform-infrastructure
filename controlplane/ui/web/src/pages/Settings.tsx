import { api, ApiError, type S } from '../api/client';
import { Badge, Kv, Time } from '../components/bits';
import { useOverlays } from '../components/overlays';
import { useCrumbs } from '../lib/chrome';
import { go, routes } from '../lib/format';
import { QueryView, useLiveQuery } from '../lib/query';

export function SettingsPage({ project }: { project: string }) {
  useCrumbs([{ label: 'Projects', href: routes.projects() }, { label: project, href: routes.project(project) }, { label: 'Settings' }]);
  const { form, toast } = useOverlays();
  const query = useLiveQuery(['settings', project], async () => {
    const { items } = await api.get<S['ProjectList']>('/projects?limit=200');
    const found = items.find((x) => x.name === project);
    if (!found) throw new ApiError(404, 'not_found', `project ${project}`);
    return found;
  }, (p) => ['PENDING', 'PROVISIONING', 'DELETING'].includes(p.status));

  async function remove(p: S['ProjectOut']) {
    const done = await form<unknown>({
      title: `Delete ${p.name}?`, submitLabel: 'Delete project',
      intro: 'The namespace and everything running in it are removed from the cluster. The history (runs, models, audit) stays in the platform database. This cannot be undone.',
      fields: [{ name: 'confirm', label: `Type ${p.name} to confirm`, required: true, placeholder: p.name }],
      submit: async (v) => {
        if (v.confirm !== p.name) throw new ApiError(422, 'invalid_argument', `Type “${p.name}” exactly to confirm`);
        return api.del(`/projects/${p.id}`);
      },
    });
    if (done !== null) { toast(`Deleting ${p.name}`); go(routes.projects()); }
  }

  return (
    <QueryView query={query}>
      {(p) => (
        <>
          <div className="page-head"><h1>Settings</h1></div>
          <div className="card">
            <h2>Project</h2>
            <Kv entries={[
              ['Name', <span className="mono">{p.name}</span>], ['Display name', p.display_name], ['Description', p.description || '—'],
              ['Status', <Badge status={p.status} />], ['Id', <span className="mono">{p.id}</span>],
              ['Created', <Time iso={p.created_at} />], ['Last change', <Time iso={p.updated_at} />],
            ]} />
          </div>
          <div className="section card danger-zone" data-testid="danger-zone">
            <h2>Delete project</h2>
            <p className="muted">Removes the project's namespace and workloads. Deployments stop serving.</p>
            <button className="btn danger" type="button" data-testid="delete-project" disabled={['DELETING', 'DELETED'].includes(p.status)}
              onClick={() => remove(p)}>Delete project…</button>
          </div>
        </>
      )}
    </QueryView>
  );
}
