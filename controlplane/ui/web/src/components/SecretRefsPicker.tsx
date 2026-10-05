import { useEffect, useState } from 'react';
import { api, ApiError, enc, type S } from '../api/client';
import { QueryView, useLiveQuery } from '../lib/query';

type Binding = { variable: string; secret: string; key: string };
export function SecretRefsPicker({ project, name, id, onChange, inputRef, allowStorage = false }: {
  project: string; name: string; id: string; onChange: (value: string) => void;
  inputRef: (element: HTMLInputElement | null) => void; allowStorage?: boolean;
}) {
  const [bindings, setBindings] = useState<Binding[]>([]);
  const [storage, setStorage] = useState('');
  const [pulls, setPulls] = useState<string[]>([]);
  const catalog = useLiveQuery(['secret-references', project], () =>
    api.get<S['SecretList']>(`/projects/${enc(project)}/secret-references`));
  const error = bindings.some(b => !b.variable || !b.secret || !b.key)
    ? 'Choose an environment name, secret and key for every binding.'
    : new Set(bindings.map(b => b.variable)).size !== bindings.length
      ? 'Environment names must be unique.' : null;
  const encoded = JSON.stringify(error ? { _error: error } : {
    env: Object.fromEntries(bindings.map(b => [b.variable, { name: b.secret, key: b.key }])),
    image_pull_secrets: pulls,
    ...(allowStorage && storage ? { storage_secret: storage } : {}),
  });
  useEffect(() => onChange(encoded), [encoded, onChange]);
  function edit(index: number, patch: Partial<Binding>) {
    setBindings(rows => rows.map((row, i) => i === index ? { ...row, ...patch } : row));
  }
  return <div id={id}>
    <input type="hidden" name={name} value={encoded} ref={inputRef} />
    <QueryView query={catalog}>{({ items }) => <>
      {bindings.map((binding, index) => <div className="field" key={index}>
        <input aria-label={`Environment name ${index + 1}`} placeholder="DB_PASSWORD"
          value={binding.variable} onChange={e => edit(index, { variable: e.target.value })} />
        <select aria-label={`Secret ${index + 1}`} value={binding.secret}
          onChange={e => edit(index, { secret: e.target.value, key: '' })}>
          <option value="">Choose secret</option>
          {items.map(s => <option key={s.name} value={s.name}>{s.name}</option>)}
        </select>
        <select aria-label={`Secret key ${index + 1}`} value={binding.key}
          onChange={e => edit(index, { key: e.target.value })}>
          <option value="">Choose key</option>
          {items.find(s => s.name === binding.secret)?.keys.map(key => <option key={key}>{key}</option>)}
        </select>
        <button type="button" className="btn small" onClick={() => setBindings(rows => rows.filter((_, i) => i !== index))}>Remove binding</button>
      </div>)}
      <button type="button" className="btn small" disabled={!items.length}
        onClick={() => setBindings(rows => [...rows, { variable: '', secret: '', key: '' }])}>Add environment secret</button>
      <fieldset><legend>Private registry credentials</legend>
        {items.filter(s => s.kind === 'kubernetes.io/dockerconfigjson').map(s => <label key={s.name}>
          <input type="checkbox" checked={pulls.includes(s.name)} onChange={e => setPulls(names =>
            e.target.checked ? [...names, s.name] : names.filter(n => n !== s.name))} /> {s.name}
        </label>)}
      </fieldset>
      {allowStorage && <label>Model artifact credentials (S3/MinIO)
        <select aria-label="Storage secret" value={storage} onChange={e => setStorage(e.target.value)}>
          <option value="">No storage credentials</option>
          {items.filter(s => s.kind === 'Opaque' && s.keys.includes('AWS_ACCESS_KEY_ID') && s.keys.includes('AWS_SECRET_ACCESS_KEY'))
            .map(s => <option key={s.name} value={s.name}>{s.name}</option>)}
        </select>
      </label>}
      {!items.length && <p className="muted">No project secrets. An admin can create them in Settings → Secrets.</p>}
    </>}</QueryView>
    {error && <small className="hint">{error}</small>}
  </div>;
}

export function secretRefsFromForm(value: string | undefined): S['SecretRefsIn'] {
  const refs = JSON.parse(value || '{}');
  if (refs._error) throw new ApiError(422, 'invalid_argument', refs._error);
  return refs;
}
