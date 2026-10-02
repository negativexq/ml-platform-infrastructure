import { useQueryClient } from '@tanstack/react-query';
import { api, ApiError, enc, type S } from '../api/client';
import { Badge, Empty, Kv, Table, Time } from '../components/bits';
import { useAccess, useMe } from '../lib/me';
import { useOverlays } from '../components/overlays';
import { useCrumbs } from '../lib/chrome';
import { go, routes } from '../lib/format';
import { QueryView, useLiveQuery } from '../lib/query';

export function SettingsPage({ project }: { project: string }) {
  useCrumbs([{ label: 'Projects', href: routes.projects() }, { label: project, href: routes.project(project) }, { label: 'Settings' }]);
  const { form, toast } = useOverlays();
  const access = useAccess(project);
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
          <Members project={project} />
          <div className="section card danger-zone" data-testid="danger-zone">
            <h2>Delete project</h2>
            <p className="muted">Removes the project's namespace and workloads. Deployments stop serving.</p>
            <button className="btn danger" type="button" data-testid="delete-project" disabled={['DELETING', 'DELETED'].includes(p.status) || !access.may('admin')}
              title={access.why('admin')}
              onClick={() => remove(p)}>Delete project…</button>
          </div>
        </>
      )}
    </QueryView>
  );
}

const ROLES = [
  { value: 'viewer', label: 'Viewer: reads everything' },
  { value: 'operator', label: 'Operator: runs, deploys, rolls out' },
  { value: 'admin', label: 'Admin: also members, thresholds, deletion' },
];

/** Who can do what here. Admins change it; everyone in the project can see it. */
function Members({ project }: { project: string }) {
  const p = enc(project);
  const access = useAccess(project);
  const me = useMe();
  const client = useQueryClient();
  const { form, confirm, toast } = useOverlays();
  const query = useLiveQuery(['members', project], () => api.get<S['MemberList']>(`/projects/${p}/members`));
  const admin = access.may('admin');

  const refresh = async () => { await client.invalidateQueries({ queryKey: ['members', project] }); await client.invalidateQueries({ queryKey: ['me'] }); };
  async function change(subject: string, role: string) {
    try { await api.put(`/projects/${p}/members/${enc(subject)}`, { role }); toast(`${subject} is now ${role}`); }
    catch (e) { toast(e instanceof Error ? e.message : 'Could not change the role', 'bad'); }
    await refresh();
  }
  async function remove(subject: string) {
    if (!(await confirm({ title: `Remove ${subject}?`, body: 'They lose access to this project at their next request.', confirmLabel: 'Remove', danger: true }))) return;
    try { await api.del(`/projects/${p}/members/${enc(subject)}`); toast(`${subject} removed`); }
    catch (e) { toast(e instanceof Error ? e.message : 'Could not remove', 'bad'); }
    await refresh();
  }
  async function add() {
    const done = await form<unknown>({
      title: 'Add a member', submitLabel: 'Add',
      intro: 'A user by the name they sign in with, or a group from your identity provider: everyone in the group gets the role.',
      fields: [
        { name: 'kind', label: 'Who', required: true, value: 'user', options: [{ value: 'user', label: 'A user' }, { value: 'group', label: 'A group' }] },
        { name: 'name', label: 'Name', required: true, placeholder: 'alice  or  ml-team', pattern: '^[^\\s:]\\S*$', hint: 'No spaces. Users: the username (or email) they sign in with.' },
        { name: 'role', label: 'Role', required: true, value: 'viewer', options: ROLES },
      ],
      submit: (v) => {
        const subject = `${v.kind}:${v.name}`;
        return api.put(`/projects/${p}/members/${enc(subject)}`, { role: v.role });
      },
    });
    if (done) { toast('Member added'); await refresh(); }
  }

  return (
    <div className="section card" data-testid="members">
      <div className="section-head">
        <h2>Members</h2>
        <button className="btn small primary" type="button" data-testid="add-member" disabled={!admin} title={access.why('admin')} onClick={add}>Add member</button>
      </div>
      {me.data?.auth === 'none' && <p className="muted small">Sign-in is off on this server, so roles are not enforced; they take effect once sign-in is on.</p>}
      <QueryView query={query}>
        {({ items }) => items.length === 0 ? <Empty>No members yet.</Empty> : (
          <Table head={['Member', 'Kind', 'Role', 'Since', '']}>
            {items.map((m) => (
              <tr key={m.subject} data-testid="member-row" data-subject={m.subject}>
                <td className="mono">{m.name}{me.data && m.kind === 'user' && m.name === me.data.username && <span className="muted small"> (you)</span>}</td>
                <td className="muted">{m.kind}</td>
                <td>{admin ? (
                  <select value={m.role} aria-label={`Role of ${m.subject}`} data-testid="member-role" onChange={(e) => change(m.subject, e.target.value)}>
                    {ROLES.map((r) => <option key={r.value} value={r.value}>{r.value}</option>)}
                  </select>) : <span className="chip">{m.role}</span>}
                </td>
                <td><Time iso={m.created_at} /></td>
                <td className="num">{admin && <button className="btn small" type="button" data-testid="remove-member" onClick={() => remove(m.subject)}>Remove</button>}</td>
              </tr>))}
          </Table>)}
      </QueryView>
      <p className="muted small">{`Your role here: ${access.role ?? 'none'}. Changes apply at the next request; group membership comes from the identity provider and applies at the next sign-in.`}</p>
    </div>
  );
}
