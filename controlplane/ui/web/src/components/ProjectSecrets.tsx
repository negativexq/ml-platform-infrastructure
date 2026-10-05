import { api, ApiError, enc, type S } from '../api/client';
import { Empty, Table } from './bits';
import { useOverlays } from './overlays';
import { useAccess } from '../lib/me';
import { QueryView, useAct, useLiveQuery } from '../lib/query';

type Secret = S['SecretOut'];
export function ProjectSecrets({ project }: { project: string }) {
  const access = useAccess(project);
  return access.may('admin') ? <SecretManagement project={project} /> : null;
}

function SecretManagement({ project }: { project: string }) {
  const { form, confirm, toast } = useOverlays();
  const act = useAct();
  const base = `/projects/${enc(project)}/secrets`;
  const query = useLiveQuery(['secrets', project], () => api.get<S['SecretList']>(base));

  async function write(secret?: Secret) {
    const result = await form<Secret>({
        title: secret ? `Rotate ${secret.name}` : 'Create project secret', submitLabel: secret ? 'Rotate' : 'Create',
        intro: 'Values are write-only. Rotation replaces the values of all keys. Running containers keep their current environment until restarted; new starts use the updated values.',
        fields: [
          ...(!secret ? [{ name: 'name', label: 'Secret name', required: true, placeholder: 'training-credentials' },
            { name: 'kind', label: 'Type', value: 'Opaque', options: [{ value: 'Opaque', label: 'Key/value secret' }, { value: 'kubernetes.io/dockerconfigjson', label: 'Registry credentials' }] }] : []),
          { name: 'annotations', label: 'S3 storage settings (JSON)', type: 'textarea', value: JSON.stringify(secret?.annotations || {}), hint: 'Optional serving.kserve.io/s3-endpoint, s3-region and s3-usehttps annotations. Contains endpoint settings, never credentials.' },
          { name: 'values', label: 'New values (JSON object)', type: 'password', required: true,
            hint: secret ? `Provide all keys: ${secret.keys.join(', ')}. Existing values cannot be read back.` : 'Example: {"AWS_ACCESS_KEY_ID":"…","AWS_SECRET_ACCESS_KEY":"…"}. Registry secrets use a .dockerconfigjson key whose value is a JSON string.' },
        ],
        submit: (v) => {
          let values: Record<string, string>;
          let annotations: Record<string, string>;
          try {
            values = JSON.parse(v.values || '');
            annotations = JSON.parse(v.annotations || '{}');
            if (!annotations || Array.isArray(annotations) || typeof annotations !== 'object' || Object.values(annotations).some(x => typeof x !== 'string')) throw new Error();
            if (!values || Array.isArray(values) || typeof values !== 'object' || Object.values(values).some(x => typeof x !== 'string')) throw new Error();
          } catch { throw new ApiError(422, 'invalid_argument', 'Values must be a JSON object of strings'); }
          return secret ? api.put<Secret>(`${base}/${enc(secret.name)}`, { values, annotations, kind: secret.kind, expected_version: secret.version })
            : api.post<Secret>(`${base}/${enc(v.name || '')}`, { values, annotations, kind: v.kind });
        },
      });
    if (result) { toast(secret ? 'Secret rotated' : 'Secret created'); await query.refetch(); }
  }

  return <div className="section card" data-testid="project-secrets">
    <div className="section-head"><h2>Secrets</h2><button className="btn" onClick={() => write()}>Create secret</button></div>
    <p className="muted">Project credentials for workloads and private registries. Only names, keys and versions are shown; values are never returned.</p>
    <QueryView query={query}>{({ items }) => items.length ? <Table head={['Name', 'Keys', 'Type', 'Version', 'Used by', 'Actions']}>
      {items.map(s => <tr key={s.name}><td>{s.name}</td><td>{s.keys.join(', ')}</td><td>{s.kind}</td><td>{s.version}</td><td>{(s.used_by || []).length ? (s.used_by || []).map(u => `${u.kind}: ${u.name}${u.revision ? ` r${u.revision}` : ''}`).join(', ') : 'Unused'}</td><td>
        <button className="btn small" onClick={() => write(s)}>Rotate</button>{' '}
        <button className="btn small danger" onClick={async () => {
          if (await confirm({ title: `Delete ${s.name}?`, body: 'Deletion is blocked if an immutable workload references this secret. Existing history remains.', confirmLabel: 'Delete', danger: true }))
            await act(() => api.del(`${base}/${enc(s.name)}?expected_version=${enc(s.version)}`), 'Secret deleted');
        }}>Delete</button>
      </td></tr>)}
    </Table> : <Empty>No project secrets yet.</Empty>}</QueryView>
  </div>;
}
