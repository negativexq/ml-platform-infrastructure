import { useState } from 'react';
import { api, enc, type S } from '../api/client';
import { Snippet } from './bits';

const EXAMPLE = '{\n  "instances": [[1.0, 2.0, 3.0]]\n}';

/** Send one request through the platform to the endpoint: the quickest "is it actually serving?". */
export function TryIt({ project, endpoint, allowed = true }: { project: string; endpoint: S['EndpointOut']; allowed?: boolean }) {
  const [body, setBody] = useState(EXAMPLE);
  const [result, setResult] = useState<{ ok: boolean; text: string; ms: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const ready = endpoint.status === 'READY';
  const path = `/projects/${enc(project)}/endpoints/${enc(endpoint.name)}/predict`;

  async function send() {
    let payload: unknown;
    try { payload = JSON.parse(body); } catch (e) {
      setResult({ ok: false, text: `Not valid JSON: ${e instanceof Error ? e.message : ''}`, ms: 0 });
      return;
    }
    setBusy(true);
    const started = performance.now();
    try {
      const response = await api.post<unknown>(path, payload);
      setResult({ ok: true, text: JSON.stringify(response, null, 2), ms: performance.now() - started });
    } catch (e) {
      setResult({ ok: false, text: e instanceof Error ? e.message : 'Request failed', ms: performance.now() - started });
    } finally { setBusy(false); }
  }

  return (
    <div className="section card" data-testid="try-it">
      <h2>Try it</h2>
      <p className="muted small">Sends one request to <span className="mono">{endpoint.name}</span> through the platform. Counts as real traffic during a canary.</p>
      <div className="cols">
        <div>
          <label className="field-label" htmlFor="try-body">Request body</label>
          <textarea id="try-body" className="code" rows={6} spellCheck={false} value={body} onChange={(e) => setBody(e.target.value)} data-testid="try-body" />
          <div className="dlg-actions" style={{ justifyContent: 'flex-start' }}>
            <button className="btn primary" type="button" disabled={!ready || busy || !allowed} data-testid="try-send" onClick={send}
              title={!allowed ? 'Needs the operator role in this project' : ready ? '' : `The endpoint is ${endpoint.status.toLowerCase()}`}>{busy ? 'Sending…' : 'Send'}</button>
          </div>
        </div>
        <div>
          <span className="field-label">Response{result && result.ms > 0 && <span className="muted small">{` · ${Math.round(result.ms)} ms`}</span>}</span>
          <pre className={`logs${result && !result.ok ? ' bad' : ''}`} data-testid="try-result">{result ? result.text : ready ? '(send a request)' : `The endpoint is ${endpoint.status.toLowerCase()}.`}</pre>
        </div>
      </div>
      <Snippet label="The same request with curl" code={`curl -X POST ${location.origin}${path} \\\n  -H 'content-type: application/json' \\\n  -d '${body.replace(/\s*\n\s*/g, ' ')}'`} />
    </div>
  );
}
