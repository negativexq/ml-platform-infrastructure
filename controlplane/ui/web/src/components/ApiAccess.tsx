import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { api, enc, type S } from '../api/client';
import { useAccess } from '../lib/me';
import { num, shellQuote } from '../lib/format';
import { QueryView, useAct } from '../lib/query';
import { Sparkline } from './charts/Sparkline';
import { TrendChart } from './charts/TrendChart';
import { CopyButton, Empty, Kv, Snippet, Time } from './bits';
import { Modal, useOverlays } from './overlays';

const EXPIRY: [string, string, number | null][] = [
  ['never', 'Never', null], ['30d', 'In 30 days', 30], ['90d', 'In 90 days', 90], ['365d', 'In a year', 365],
];
const SLUG = '[a-z][a-z0-9]*(-[a-z0-9]+)*';
const RANGES: [string, number][] = [['1h', 60], ['6h', 360], ['24h', 1440]];

const stateChip = (state: string) => (
  <span className="status-chip" data-state={state}>
    <span aria-hidden="true" style={{ color: state === 'active' ? 'var(--status-good)' : 'var(--status-neutral)' }}>
      {state === 'active' ? '●' : '○'}
    </span>{state}
  </span>
);

/** Issuing a key, then showing its secret exactly once. */
function useIssueKey(project: string) {
  const { form } = useOverlays();
  const [secret, setSecret] = useState<S['ApiKeyCreated'] | null>(null);
  const act = useAct();
  async function issue(endpoint?: string) {
    const created = await form<S['ApiKeyCreated']>({
      title: 'New API key',
      intro: 'A key lets a service outside the platform call public endpoints through the gateway. Nothing else: it cannot read or change the project.',
      submitLabel: 'Issue key',
      fields: [
        { name: 'name', label: 'Who is it for', required: true, pattern: SLUG, placeholder: 'partner-acme', hint: 'Lowercase, digits and hyphens. Shown in usage and in the audit trail.' },
        ...(endpoint ? [] : [{ name: 'endpoints', label: 'Endpoints it may call', required: true, placeholder: 'credit-risk-prod, ranker-staging', hint: 'Comma-separated endpoint names.' }]),
        { name: 'limit', label: 'Its own limit per minute (requests, or tokens for an LLM)', pattern: '[0-9]{1,7}', placeholder: 'the endpoint’s limit', hint: 'Optional. Leave empty to share the endpoint’s limit.' },
        { name: 'expires', label: 'Expires', value: 'never', options: EXPIRY.map(([value, label]) => ({ value, label })) },
      ],
      preview: (v) => {
        const targets = endpoint ? [endpoint] : (v.endpoints ?? '').split(',').map((s) => s.trim()).filter(Boolean);
        const days = EXPIRY.find(([key]) => key === v.expires)?.[2];
        return (
          <>
            <p>{`${v.name || 'This key'} will be able to call ${targets.length ? targets.join(', ') : 'no endpoint yet'}`}
              {v.limit ? `, at most ${v.limit} units (requests, or tokens for an LLM) per minute` : ', within each endpoint’s limit'}
              {days ? `, until ${new Date(Date.now() + days * 86_400_000).toLocaleDateString()}` : ', with no expiry'}.</p>
            <p>The secret is shown once, right after this. Revoking it takes effect within seconds.</p>
          </>
        );
      },
      submit: (v) => {
        const days = EXPIRY.find(([key]) => key === v.expires)?.[2];
        return api.post<S['ApiKeyCreated']>(`/projects/${enc(project)}/api-keys`, {
          name: v.name,
          endpoints: endpoint ? [endpoint] : (v.endpoints ?? '').split(',').map((s) => s.trim()).filter(Boolean),
          units_per_minute: v.limit ? Number(v.limit) : null,
          expires_at: days ? new Date(Date.now() + days * 86_400_000).toISOString() : null,
        });
      },
    });
    if (created) { setSecret(created); await act(async () => undefined, `Key ${created.key.name} issued`); }
  }
  const dialog = (
    <Modal open={secret !== null} className="wide" onClose={() => setSecret(null)}>
      {secret && (
        <div data-testid="key-secret-dialog">
          <h2>{`Key for ${secret.key.name}`}</h2>
          <div className="alert">Copy it now and store it in a secret manager. It is not shown again; if it is lost, revoke it and issue a new one.</div>
          <div className="secret-row">
            <input className="mono" readOnly value={secret.secret} aria-label="API key" data-testid="key-secret" onFocus={(e) => e.currentTarget.select()} />
            <CopyButton text={secret.secret} what="API key" />
          </div>
          <div className="dlg-actions">
            <button className="btn primary" type="button" onClick={(e) => (e.currentTarget.closest('dialog') as HTMLDialogElement).close()}>I have stored it</button>
          </div>
        </div>)}
    </Modal>
  );
  return { issue, dialog };
}

function useRevoke(project: string) {
  const { confirm } = useOverlays();
  const act = useAct();
  return async (key: S['ApiKeyOut']) => {
    const lastUse = key.last_used_at ? `It was last used ${new Date(key.last_used_at).toLocaleString()}.` : 'It has never been used.';
    if (await confirm({
      title: `Revoke ${key.name}?`, confirmLabel: 'Revoke key', danger: true,
      body: `Calls with this key are refused within seconds (${key.endpoints.join(', ')}). ${lastUse} This cannot be undone; a new key can be issued.`,
    })) await act(() => api.del(`/projects/${enc(project)}/api-keys/${key.key_id}`), `Key ${key.name} revoked`);
  };
}

function KeysTable({ keys, allowed, why, onRevoke, showEndpoints }: {
  keys: S['ApiKeyOut'][]; allowed: boolean; why?: string; onRevoke: (k: S['ApiKeyOut']) => void; showEndpoints?: boolean;
}) {
  return (
    <div className="table-scroll">
      <table className="t" data-testid="keys-table">
        <thead><tr><th>Name</th><th>Key</th>{showEndpoints && <th>Endpoints</th>}<th className="num">Limit / min</th><th>Last used</th><th>Expires</th><th>State</th><th /></tr></thead>
        <tbody>
          {[...keys].sort((a, b) => Number(a.state !== 'active') - Number(b.state !== 'active')).map((k) => (
            <tr key={k.key_id} data-key={k.name} className={k.state === 'active' ? '' : 'inactive'}>
              <td>{k.name}<div className="muted small">{`by ${k.created_by}`}</div></td>
              <td className="mono small">{`mlp_live_${k.key_id}_…`}</td>
              {showEndpoints && <td className="small">{k.endpoints.join(', ')}</td>}
              <td className="num">{k.units_per_minute ?? <span className="muted">endpoint’s</span>}</td>
              <td>{k.last_used_at ? <Time iso={k.last_used_at} /> : <span className="muted">never</span>}</td>
              <td>{k.expires_at ? <Time iso={k.expires_at} /> : <span className="muted">never</span>}</td>
              <td>{stateChip(k.state)}</td>
              <td className="num">{k.state === 'active' && (
                <button className="btn small" type="button" data-testid="revoke-key" disabled={!allowed} title={why ?? 'Refuse calls with this key from now on'}
                  onClick={() => onRevoke(k)}>Revoke</button>)}</td>
            </tr>))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * How an endpoint is reached from outside: whether it is public, the URL and limits callers
 * get, the keys that can call it, and how they are using it. Opening, closing and limits are
 * admin decisions, previewed before they are made.
 */
export function ApiAccessCard({ project, endpoint }: { project: string; endpoint: S['EndpointOut'] }) {
  const access = useAccess(project);
  const { confirm, form } = useOverlays();
  const act = useAct();
  const { issue, dialog } = useIssueKey(project);
  const revoke = useRevoke(project);
  const path = `/projects/${enc(project)}/endpoints/${enc(endpoint.name)}`;
  const query = useQuery({ queryKey: ['api-access', project, endpoint.name], queryFn: () => api.get<S['EndpointAccessOut']>(`${path}/access`), refetchInterval: 30_000 });
  const admin = access.may('admin');
  const client = useQueryClient();

  return (
    <div className="section card" data-testid="api-access">
      <QueryView query={query}>
        {(a) => {
          const isPublic = a.exposure === 'public';
          const active = a.keys.filter((k) => k.state === 'active');
          const L = a.limits;
          const llm = a.protocol === 'openai';
          const fn = a.protocol === 'http';
          const unit = llm ? 'tokens' : 'requests';
          async function toggle() {
            const opening = !isPublic;
            const ok = await confirm({
              title: opening ? `Make ${a.endpoint} public?` : `Make ${a.endpoint} internal?`,
              confirmLabel: opening ? 'Make public' : 'Make internal', danger: !opening,
              body: opening
                ? `Anyone holding a key for ${a.endpoint} can call it${a.public_url ? ` at ${a.public_url}` : ' through the gateway'}, up to ${L.units_per_minute} ${unit} per minute in total. ${active.length} active key${active.length === 1 ? '' : 's'} can already call it.`
                : `Calls through the gateway are refused within seconds. ${active.length} active key${active.length === 1 ? ' loses' : 's lose'} access; the keys are kept and work again if you reopen it. Calls inside the platform are not affected.`,
            });
            if (ok) await act(() => api.patch(path, { exposure: opening ? 'public' : 'internal' }), opening ? `${a.endpoint} is public` : `${a.endpoint} is internal`);
          }
          async function editLimits(peak: number | null) {
            await form({
              title: `Limits for ${a.endpoint}`, submitLabel: 'Save limits',
              intro: 'The gateway enforces these for every caller together. A key can have a smaller limit of its own.',
              fields: [
                { name: 'units_per_minute', label: `${llm ? 'Tokens' : 'Requests'} per minute, all callers`, required: true, pattern: '[0-9]{1,7}', value: String(L.units_per_minute) },
                { name: 'max_body_kb', label: 'Largest request body, KB', required: true, pattern: '[0-9]{1,5}', value: String(L.max_body_kb) },
                { name: 'timeout_seconds', label: 'Timeout, seconds', required: true, pattern: '[0-9]{1,3}', value: String(L.timeout_seconds) },
              ],
              preview: (v) => {
                const limit = Number(v.units_per_minute);
                if (!limit) return null;
                return (
                  <>
                    {peak != null && (
                      <p className={peak > limit ? 'fail' : 'pass'}>
                        {peak > limit
                          ? `The busiest minute in the last hour used ${num(peak, 0)} ${unit}: at ${limit} some calls would have been refused (429).`
                          : `The busiest minute in the last hour used ${num(peak, 0)} ${unit}, within the new limit.`}
                      </p>)}
                    <p>{`Calls over ${limit} per minute get 429 with Retry-After; bodies over ${v.max_body_kb} KB get 413; no answer within ${v.timeout_seconds} s is a 504.`}</p>
                  </>
                );
              },
              submit: (v) => api.patch(path, { exposure: a.exposure, limits: {
                units_per_minute: Number(v.units_per_minute), max_body_kb: Number(v.max_body_kb), timeout_seconds: Number(v.timeout_seconds),
              } }),
            }).then(async (done) => { if (done !== null) await act(async () => undefined, 'Limits saved'); });
          }
          const base = a.public_url?.replace(/\/chat\/completions$/, '');
          const curl = a.public_url && (llm ? [
            `curl -N ${shellQuote(a.public_url)} \\`,
            `  -H 'Authorization: Bearer $MLP_API_KEY' \\`,
            `  -H 'Content-Type: application/json' \\`,
            `  -d '{"messages": [{"role": "user", "content": "Hello"}], "max_tokens": 256, "stream": true}'`,
          ] : [
            `curl ${shellQuote(a.public_url)} \\`,
            `  -H 'Authorization: Bearer $MLP_API_KEY' \\`,
            `  -H 'Content-Type: application/json' \\`,
            fn ? `  -d '{"ticket": "My card was declined"}'` : `  -d '{"instances": [[1.0, 2.0, 3.0]]}'`,
          ]).join('\n');
          const python = a.public_url && (llm ? [
            'import os',
            'from openai import OpenAI',
            '',
            'client = OpenAI(',
            `    base_url="${base}",`,
            '    api_key=os.environ["MLP_API_KEY"],',
            ')',
            'stream = client.chat.completions.create(',
            `    model="${a.endpoint}",  # any name: the gateway addresses the served model`,
            '    messages=[{"role": "user", "content": "Hello"}],',
            '    stream=True,',
            ')',
            'for chunk in stream:',
            '    if chunk.choices:',
            '        print(chunk.choices[0].delta.content or "", end="")',
          ] : [
            'import os, requests',
            '',
            'reply = requests.post(',
            `    "${a.public_url}",`,
            '    headers={"Authorization": f"Bearer {os.environ[\'MLP_API_KEY\']}"},',
            fn ? '    json={"ticket": "My card was declined"},' : '    json={"instances": [[1.0, 2.0, 3.0]]},',
            `    timeout=${L.timeout_seconds},`,
            ')',
            'reply.raise_for_status()',
            fn ? 'print(reply.json())' : 'print(reply.json()["predictions"])',
          ]).join('\n');
          return (
            <>
              <div className="section-head">
                <h2>API access<span className={`chip exposure ${a.exposure}`} data-testid="exposure">{isPublic ? 'Public' : 'Internal'}</span></h2>
                <div className="actions-inline">
                  <button className="btn small" type="button" data-testid="edit-limits" disabled={!admin} title={access.why('admin') ?? 'Change the gateway limits'}
                    onClick={() => editLimits(peakOf(client.getQueryData<S['EndpointUsageOut']>(['usage', project, endpoint.name, 60])))}>Edit limits</button>
                  <button className={`btn small${isPublic ? '' : ' primary'}`} type="button" data-testid="toggle-exposure" disabled={!admin}
                    title={access.why('admin') ?? (isPublic ? 'Stop calls from outside' : 'Let keys call it through the gateway')} onClick={toggle}>
                    {isPublic ? 'Make internal' : 'Make public'}</button>
                </div>
              </div>
              {isPublic ? (
                a.public_url ? (
                  <div className="public-url" data-testid="public-url">
                    <span className="method">POST</span><span className="mono">{a.public_url}</span><CopyButton text={a.public_url} what="URL" />
                  </div>
                ) : <p className="muted">No gateway address is configured (CP_GATEWAY_URL), so the public URL cannot be shown.</p>
              ) : <p className="muted">Only reachable inside the platform. Make it public to let services outside call it with a key.</p>}
              <Kv entries={[
                ['Limit', `${L.units_per_minute} ${unit} per minute, all callers together`],
                ['Request body', `up to ${L.max_body_kb} KB`],
                ['Timeout', `${L.timeout_seconds} s`],
                ['Protocol', llm
                  ? `OpenAI-compatible chat: POST …/${a.operation} with {"messages": [...]}, streamed with "stream": true`
                  : fn ? `Any JSON: POST …/${a.operation}; the function's own container answers`
                  : `${a.protocol}: POST …/${a.operation} with {"instances": [...]}`],
              ]} />
              {curl && <Snippet label={llm ? 'Call it with curl (streamed)' : 'Call it with curl'} code={curl} />}
              {python && <Snippet label={llm ? 'Call it with the OpenAI SDK' : 'Call it from Python'} code={python} />}
              <div className="section-head sub-section">
                <h3>{`Keys that can call it (${active.length} active)`}</h3>
                <button className="btn small" type="button" data-testid="new-key" disabled={!admin} title={access.why('admin') ?? 'Issue a key for this endpoint'}
                  onClick={() => issue(a.endpoint)}>New key</button>
              </div>
              {a.keys.length
                ? <KeysTable keys={a.keys} allowed={admin} why={access.why('admin')} onRevoke={revoke} />
                : <Empty>No keys yet. Issue one per caller, so usage and revocation are per caller too.</Empty>}
              <Usage project={project} endpoint={a.endpoint} limit={L.units_per_minute} />
            </>
          );
        }}
      </QueryView>
      {dialog}
    </div>
  );
}

/** All callers' units (requests or tokens) per minute, summed at each moment. */
function totalsOf(u: S['EndpointUsageOut'] | undefined) {
  const totals = new Map<number, number>();
  for (const c of u?.callers ?? []) for (const p of c.points) totals.set(Date.parse(p.at), (totals.get(Date.parse(p.at)) ?? 0) + p.units);
  return [...totals.entries()].sort((a, b) => a[0] - b[0]).map(([t, v]) => ({ t, v }));
}

/** The busiest minute in the last hour's usage (if it was loaded), for the limits preview. */
function peakOf(u: S['EndpointUsageOut'] | undefined) {
  const total = totalsOf(u);
  return u?.available && total.length ? Math.max(...total.map((p) => p.v)) : null;
}

/** Calls through the gateway over time against the endpoint's limit, and who made them. */
function Usage({ project, endpoint, limit }: { project: string; endpoint: string; limit: number }) {
  const [range, setRange] = useState('1h');
  const minutes = RANGES.find(([k]) => k === range)?.[1] ?? 60;
  const query = useQuery({
    queryKey: ['usage', project, endpoint, minutes],
    queryFn: () => api.get<S['EndpointUsageOut']>(`/projects/${enc(project)}/endpoints/${enc(endpoint)}/usage?minutes=${minutes}`),
    placeholderData: keepPreviousData, refetchInterval: 30_000,
  });
  const u = query.data;
  const total = totalsOf(u);
  const tokens = u?.unit === 'tokens';
  const Unit = tokens ? 'Tokens' : 'Requests';
  const peak = total.length ? Math.max(...total.map((p) => p.v)) : 0;
  // Draw the limit only when it is near the traffic; far above, it would flatten the line.
  const near = limit <= peak * 3;
  const all = (u?.callers ?? []).reduce((s, c) => s + c.units, 0);
  const start = u ? Date.parse(u.start) : Date.now() - minutes * 60000, end = u ? Date.parse(u.end) : Date.now();
  return (
    <div className="usage" data-testid="usage">
      <div className="section-head sub-section">
        <h3>Usage</h3>
        <div className="seg" role="group" aria-label="Usage range">
          {RANGES.map(([key]) => <button key={key} type="button" aria-pressed={key === range} data-usage-range={key} onClick={() => setRange(key)}>{key}</button>)}
        </div>
      </div>
      {!u ? <p className="muted">Loading…</p>
        : !u.available ? <div className="alert">{`Usage unavailable: ${u.error}`}</div>
        : !u.callers.length ? <p className="muted">No calls through the gateway in this range.</p>
        : (
          <div className={query.isPlaceholderData ? 'refetching' : ''}>
            <div className="trends">
              <TrendChart testid="usage-total" title={near ? `${Unit} per minute, all callers` : `${Unit} per minute, all callers: peak ${num(peak, 0)}, ${num((100 * peak) / limit, 0)}% of the ${limit} limit`}
                start={start} end={end} markers={[]}
                format={(v) => num(v, v < 10 ? 1 : 0)} reference={near ? { value: limit, label: `limit ${limit}` } : undefined}
                series={[{ key: 'all', label: 'all callers', short: '', color: 'var(--series-1)', points: total }]} />
              {u.p95_latency_ms.length > 0 && (
                <TrendChart testid="usage-p95" title="Latency p95 through the gateway" start={start} end={end} markers={[]}
                  format={(v) => `${num(v, 0)} ms`}
                  series={[{ key: 'p95', label: 'p95', short: '', color: 'var(--series-1)', points: u.p95_latency_ms.map((p) => ({ t: Date.parse(p.at), v: p.value })) }]} />)}
            </div>
            <div className="table-scroll">
              <table className="t" data-testid="usage-callers">
                <thead><tr><th>Caller</th><th className="num">{Unit}</th>{tokens && <><th className="num">Prompt</th><th className="num">Completion</th></>}<th className="num">Share</th><th className="num">Refused calls</th><th className="num">Failed calls</th><th>{`${Unit} per minute`}</th></tr></thead>
                <tbody>
                  {u.callers.map((c) => (
                    <tr key={c.caller} data-caller={c.caller}>
                      <td>{c.caller}</td>
                      <td className="num">{num(c.units, 0)}</td>
                      {tokens && <>
                        <td className="num" data-testid="prompt-tokens">{c.prompt_tokens == null ? '—' : num(c.prompt_tokens, 0)}</td>
                        <td className="num" data-testid="completion-tokens">{c.completion_tokens == null ? '—' : num(c.completion_tokens, 0)}</td>
                      </>}
                      <td className="num">{all ? `${num((100 * c.units) / all, 0)}%` : '—'}</td>
                      <td className="num" title="Refused by the gateway: limits, keys, body size (4xx)">{num(c.rejected, 0)}</td>
                      <td className="num" title="The model failed or timed out (5xx)">{num(c.errors, 0)}</td>
                      <td><Sparkline points={c.points.map((p) => ({ t: Date.parse(p.at), v: p.units }))} threshold={null}
                        label={`${c.caller}: ${num(c.units, 0)} ${tokens ? 'tokens' : 'requests'} in the range`} /></td>
                    </tr>))}
                </tbody>
              </table>
            </div>
          </div>)}
    </div>
  );
}

/** Every key in the project, for the admins who hand them out. */
export function ApiKeys({ project }: { project: string }) {
  const access = useAccess(project);
  const { issue, dialog } = useIssueKey(project);
  const revoke = useRevoke(project);
  const query = useQuery({ queryKey: ['api-keys', project], queryFn: () => api.get<S['ApiKeyList']>(`/projects/${enc(project)}/api-keys`) });
  return (
    <div className="section card" data-testid="api-keys">
      <div className="section-head">
        <h2>API keys</h2>
        <button className="btn small" type="button" data-testid="new-project-key" disabled={!access.may('admin')} title={access.why('admin') ?? 'Issue a key'} onClick={() => issue()}>New key</button>
      </div>
      <p className="muted small">For services outside the platform. A key calls the public endpoints it names and nothing else. Open an endpoint to the outside from its deployment page.</p>
      <QueryView query={query}>
        {(list) => list.items.length
          ? <KeysTable keys={list.items} allowed={access.may('admin')} why={access.why('admin')} onRevoke={revoke} showEndpoints />
          : <Empty>No API keys in this project.</Empty>}
      </QueryView>
      {dialog}
    </div>
  );
}
