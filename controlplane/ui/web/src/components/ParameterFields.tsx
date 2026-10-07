import { useState, type RefCallback } from 'react';
import { ApiError } from '../api/client';
export type ParameterSchema = { properties?: Record<string, { type?: string; enum?: unknown[]; default?: unknown; title?: string; description?: string; format?: string; minimum?: number; maximum?: number }>; required?: string[] };
export function parseParameterObject(text: string | undefined): Record<string, unknown> {
  try { const value: unknown = JSON.parse(text || '{}'); if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(); return value as Record<string, unknown>; }
  catch { throw new ApiError(422, 'invalid_argument', 'Parameters must be a JSON object'); }
}
export function ParameterFields({ schema, name, inputRef }: { schema: ParameterSchema; name: string; inputRef: RefCallback<HTMLInputElement> }) {
  const [values, setValues] = useState<Record<string, string>>({});
  const resolved: Record<string, unknown> = {};
  let invalid = false;
  for (const [key, prop] of Object.entries(schema.properties ?? {})) {
    const text = values[key]; if (text === undefined || text === '') continue;
    try { resolved[key] = prop.type === 'string' ? text : JSON.parse(text); } catch { invalid = true; }
  }
  return <div className="parameter-fields"><input type="hidden" name={name} ref={inputRef} value={invalid ? 'invalid' : JSON.stringify(resolved)} />
    {!Object.keys(schema.properties ?? {}).length && <p className="muted small">This definition has no run parameters.</p>}
    {Object.entries(schema.properties ?? {}).map(([key, prop]) => {
      const required = Boolean(schema.required?.includes(key) && prop.default === undefined);
      const value = values[key] ?? '';
      const change = (text: string) => setValues(old => ({ ...old, [key]: text }));
      const id = `parameter-${key}`;
      const options = prop.enum ?? (prop.type === 'boolean' ? [true, false] : undefined);
      return <div className="field" key={key}><label htmlFor={id}>{prop.title || key}{required && ' *'}</label>
        {options ? <select id={id} value={value} required={required} onChange={e => change(e.target.value)}><option value="">{prop.default === undefined ? 'Choose value' : `Default: ${String(prop.default)}`}</option>{options.map(option => <option key={JSON.stringify(option)} value={prop.type === 'string' ? String(option) : JSON.stringify(option)}>{String(option)}</option>)}</select>
          : prop.type === 'array' || prop.type === 'object' ? <textarea id={id} value={value} required={required} placeholder={prop.default === undefined ? 'JSON value' : JSON.stringify(prop.default)} onChange={e => change(e.target.value)} />
            : <input id={id} value={value} required={required} type={prop.type === 'number' || prop.type === 'integer' ? 'number' : prop.format === 'date' ? 'date' : 'text'} step={prop.type === 'integer' ? 1 : 'any'} min={prop.minimum} max={prop.maximum} placeholder={prop.default === undefined ? key : `Default: ${String(prop.default)}`} onChange={e => change(e.target.value)} />}
        {prop.description && <small className="hint">{prop.description}</small>}</div>;
    })}</div>;
}
